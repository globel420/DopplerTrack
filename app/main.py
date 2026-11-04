from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import redis
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from jwt.exceptions import PyJWTError

from app import settings
from app.access import principal, verify_access_token
from app.analysis import AnalysisError, report_filename, safe_report_json
from app.geolocation import validate_search_area
from app.url_security import UnsafeMediaURL, validate_media_url

if settings.APP_ENV == "production":
    settings.validate_production_access()

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")
logger = logging.getLogger("dopplertrack")

redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
app = FastAPI(title="DopplerTrack", docs_url=None, redoc_url=None)
UPLOAD_CHUNK_BYTES = 32 * 1024 * 1024


def require_access(request: Request) -> str:
    if settings.APP_ENV != "production":
        request.state.principal = "local-development"
        return request.state.principal
    token = request.headers.get("Cf-Access-Jwt-Assertion", "")
    if not token:
        raise HTTPException(status_code=401, detail="Cloudflare Access authentication is required.")
    try:
        claims = verify_access_token(token)
        user = principal(claims)
    except (PyJWTError, OSError, TimeoutError):
        # Do not disclose token validation details or write claims to logs.
        raise HTTPException(status_code=401, detail="Cloudflare Access authentication could not be verified.")
    request.state.principal = user
    return user


api = APIRouter(prefix="/api", dependencies=[Depends(require_access)])


def _owner_hash(owner: str) -> str:
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()[:24]


def _rate_limit(owner: str, action: str, limit: int, window: int) -> None:
    key = f"rate:{_owner_hash(owner)}:{action}:{int(time.time()) // window}"
    try:
        count = redis_client.incr(key)
        if count == 1:
            redis_client.expire(key, window + 5)
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Rate limiter is unavailable. Try again shortly.") from exc
    if count > limit:
        raise HTTPException(status_code=429, detail="Too many requests. Please wait before trying again.")


def _job_key(job_id: str) -> str:
    if not settings.JOB_ID_PATTERN.fullmatch(job_id):
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    return f"job:{job_id}"


def _load_job(job_id: str, owner: str) -> dict[str, Any]:
    key = _job_key(job_id)
    try:
        raw = redis_client.get(key)
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Job storage is unavailable.") from exc
    if not raw:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    job = json.loads(raw)
    if not secrets.compare_digest(str(job.get("owner", "")), _owner_hash(owner)):
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    return job


def _save_job(job: dict[str, Any]) -> None:
    try:
        expires = datetime.fromisoformat(job["expires_at"]).timestamp()
        ttl = max(1, min(settings.RETENTION_SECONDS, int(expires - time.time())))
    except (KeyError, TypeError, ValueError):
        ttl = settings.RETENTION_SECONDS
    redis_client.setex(f"job:{job['id']}", ttl, json.dumps(job, separators=(",", ":")))


def _ensure_queue_room() -> None:
    try:
        queue_length = redis_client.llen("jobs:queue")
        active = redis_client.get("jobs:active")
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Job queue is unavailable.") from exc
    if queue_length + bool(active) >= settings.MAX_QUEUE:
        raise HTTPException(status_code=429, detail="The analysis queue is full. Please try again later.")


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "status": job["status"],
        "phase": job.get("phase", "quick"),
        "progress": job.get("progress", 0),
        "created_at": job.get("created_at"),
        "expires_at": job.get("expires_at"),
        "media": job.get("media"),
        "recording_datetime": job.get("recording_datetime"),
        "search_area": job.get("search_area"),
        "time_uncertainty_seconds": job.get("time_uncertainty_seconds", 30),
        "flight_lookup_consent": bool(job.get("flight_lookup_consent")),
        "quick_result": job.get("quick_result"),
        "deep_result": job.get("deep_result"),
        "error": job.get("error"),
        "attempt": job.get("attempt", 1),
        "max_attempts": 2,
        "source_type": job.get("source_type", "upload"),
    }


def _validated_scan_options(
    recording_datetime: str,
    center_lat: str,
    center_lon: str,
    search_radius_km: str,
    time_uncertainty_seconds: int,
    flight_lookup_consent: bool,
) -> tuple[str | None, dict | None, int, bool]:
    if len(recording_datetime) > 80:
        raise HTTPException(status_code=400, detail="Recording time is too long.")
    recording_datetime = recording_datetime.strip()
    if recording_datetime:
        try:
            parsed_time = datetime.fromisoformat(recording_datetime)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Use an ISO 8601 recording time with a UTC offset.") from exc
        if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
            raise HTTPException(status_code=400, detail="Include the recording time's UTC offset; the app will not infer it.")
        recording_datetime = parsed_time.astimezone(UTC).isoformat()
    if not 1 <= time_uncertainty_seconds <= 600:
        raise HTTPException(status_code=400, detail="Recording-time uncertainty must be between 1 and 600 seconds.")
    region_values = [center_lat.strip(), center_lon.strip(), search_radius_km.strip()]
    if any(len(value) > 40 for value in region_values):
        raise HTTPException(status_code=400, detail="Search-area values are too long.")
    search_area = None
    if any(region_values):
        if not all(region_values):
            raise HTTPException(status_code=400, detail="Provide latitude, longitude, and search radius together.")
        try:
            validated_area = validate_search_area(*region_values)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        search_area = {
            "center_lat": validated_area.center_lat,
            "center_lon": validated_area.center_lon,
            "radius_km": validated_area.radius_km,
        }
    if flight_lookup_consent and (not recording_datetime or not search_area):
        raise HTTPException(
            status_code=400,
            detail="Enter the recording time and bounded search area before requesting historical flight data.",
        )
    return recording_datetime or None, search_area, time_uncertainty_seconds, flight_lookup_consent


def _upload_session_key(upload_id: str) -> str:
    if not settings.JOB_ID_PATTERN.fullmatch(upload_id):
        raise HTTPException(status_code=404, detail="Upload not found or expired.")
    return f"upload:{upload_id}"


def _load_upload_session(upload_id: str, owner: str) -> dict[str, Any]:
    key = _upload_session_key(upload_id)
    try:
        raw = redis_client.get(key)
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Upload storage is unavailable.") from exc
    if not raw:
        raise HTTPException(status_code=404, detail="Upload not found or expired.")
    session = json.loads(raw)
    if not secrets.compare_digest(str(session.get("owner", "")), _owner_hash(owner)):
        raise HTTPException(status_code=404, detail="Upload not found or expired.")
    return session


def _release_owned_lock(key: str, value: str) -> None:
    try:
        redis_client.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
            1,
            key,
            value,
        )
    except redis.RedisError:
        pass


def _discard_upload_session(upload_id: str) -> None:
    try:
        redis_client.delete(_upload_session_key(upload_id))
    except redis.RedisError:
        pass
    shutil.rmtree(Path(settings.UPLOAD_DIR) / upload_id, ignore_errors=True)
    _release_owned_lock("jobs:upload-lock", upload_id)


def _finish_upload_session(upload_id: str) -> None:
    try:
        redis_client.delete(_upload_session_key(upload_id))
    except redis.RedisError:
        pass
    try:
        (Path(settings.UPLOAD_DIR) / upload_id / ".upload_session").unlink(missing_ok=True)
    except OSError:
        pass
    _release_owned_lock("jobs:upload-lock", upload_id)


async def _save_upload(upload: UploadFile, job_id: str) -> Path:
    root = Path(settings.UPLOAD_DIR) / job_id
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(root, 0o700)
    stamp = time.time()
    marker = root / ".created_at"
    marker.write_text(str(stamp), encoding="ascii")
    os.chmod(marker, 0o600)
    suffix = Path(upload.filename or "").suffix.lower()
    destination = root / f"source{suffix}"
    size = 0
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            while True:
                block = await upload.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > settings.MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail=f"The upload exceeds {settings.MAX_UPLOAD_MB} MiB.")
                handle.write(block)
        if size == 0:
            raise HTTPException(status_code=400, detail="The uploaded file is empty.")
        return destination
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    finally:
        await upload.close()


@api.post("/uploads")
async def create_upload_session(request: Request) -> dict:
    owner = request.state.principal
    _rate_limit(owner, "upload", 3, 3600)
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Upload session details must be valid JSON.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Upload session details must be a JSON object.")

    raw_filename = body.get("filename")
    if not isinstance(raw_filename, str) or len(raw_filename) > 255:
        raise HTTPException(status_code=400, detail="Choose a supported video filename.")
    filename = Path(raw_filename.replace("\\", "/")).name
    extension = Path(filename).suffix.lower()
    if extension not in settings.ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Choose an MP4, MOV, MKV, or WebM video.")
    size = body.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise HTTPException(status_code=400, detail="The video size must be a positive whole number of bytes.")
    if size > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"The upload exceeds the {settings.MAX_UPLOAD_MB} MiB limit.")
    uncertainty = body.get("time_uncertainty_seconds", 30)
    if isinstance(uncertainty, bool) or not isinstance(uncertainty, int):
        raise HTTPException(status_code=400, detail="Recording-time uncertainty must be a whole number of seconds.")
    consent = body.get("flight_lookup_consent", False)
    if not isinstance(consent, bool):
        raise HTTPException(status_code=400, detail="Historical flight lookup consent must be true or false.")
    string_fields = ("recording_datetime", "center_lat", "center_lon", "search_radius_km")
    if any(not isinstance(body.get(field, ""), str) for field in string_fields):
        raise HTTPException(status_code=400, detail="Recording details must be text values.")
    recorded, area, uncertainty, consent = _validated_scan_options(
        body.get("recording_datetime", ""),
        body.get("center_lat", ""),
        body.get("center_lon", ""),
        body.get("search_radius_km", ""),
        uncertainty,
        consent,
    )
    _ensure_queue_room()

    upload_id = secrets.token_urlsafe(32)
    try:
        if not redis_client.set(
            "jobs:upload-lock", upload_id, ex=settings.UPLOAD_SESSION_TTL_SECONDS, nx=True
        ):
            raise HTTPException(status_code=429, detail="Another upload is being prepared. Please try again shortly.")
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Upload storage is unavailable.") from exc

    upload_root = Path(settings.UPLOAD_DIR)
    root = upload_root / upload_id
    try:
        upload_root.mkdir(parents=True, exist_ok=True)
        free_bytes = shutil.disk_usage(upload_root).free
        reserve_bytes = settings.MIN_FREE_DISK_GB * 1024 * 1024 * 1024
        if free_bytes < size + reserve_bytes:
            raise HTTPException(
                status_code=507,
                detail="There is not enough free server storage for this video while preserving the configured reserve.",
            )
        root.mkdir(mode=0o700, parents=False, exist_ok=False)
        os.chmod(root, 0o700)
        now = time.time()
        marker = root / ".created_at"
        marker.write_text(str(now), encoding="ascii")
        os.chmod(marker, 0o600)
        session_marker = root / ".upload_session"
        session_marker.write_text(str(now), encoding="ascii")
        os.chmod(session_marker, 0o600)
        source_path = root / f"source{extension}"
        descriptor = os.open(source_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.ftruncate(descriptor, size)
        finally:
            os.close(descriptor)
        session = {
            "id": upload_id,
            "owner": _owner_hash(owner),
            "filename": filename,
            "extension": extension,
            "size": size,
            "chunk_size": UPLOAD_CHUNK_BYTES,
            "chunks": [None] * ((size + UPLOAD_CHUNK_BYTES - 1) // UPLOAD_CHUNK_BYTES),
            "created_at": now,
            "recording_datetime": recorded,
            "search_area": area,
            "time_uncertainty_seconds": uncertainty,
            "flight_lookup_consent": consent,
        }
        redis_client.setex(
            _upload_session_key(upload_id),
            settings.UPLOAD_SESSION_TTL_SECONDS,
            json.dumps(session, separators=(",", ":")),
        )
        return {
            "id": upload_id,
            "chunk_size": UPLOAD_CHUNK_BYTES,
            "chunk_count": len(session["chunks"]),
            "expires_in_seconds": settings.UPLOAD_SESSION_TTL_SECONDS,
        }
    except HTTPException:
        _discard_upload_session(upload_id)
        raise
    except (OSError, redis.RedisError) as exc:
        _discard_upload_session(upload_id)
        raise HTTPException(status_code=503, detail="Upload session could not be prepared.") from exc


@api.put("/uploads/{upload_id}/chunks/{chunk_index}")
async def put_upload_chunk(upload_id: str, chunk_index: int, request: Request) -> dict:
    owner = request.state.principal
    _rate_limit(owner, "upload-chunk", 400, 3600)
    session = _load_upload_session(upload_id, owner)
    if redis_client.get("jobs:upload-lock") != upload_id:
        raise HTTPException(status_code=410, detail="The upload session expired. Start the upload again.")
    if not 0 <= chunk_index < len(session["chunks"]):
        raise HTTPException(status_code=404, detail="Upload chunk not found.")
    chunk_start = chunk_index * UPLOAD_CHUNK_BYTES
    expected_size = min(UPLOAD_CHUNK_BYTES, session["size"] - chunk_start)
    content_range = request.headers.get("content-range", "")
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
    if not match or tuple(map(int, match.groups())) != (
        chunk_start, chunk_start + expected_size - 1, session["size"]
    ):
        raise HTTPException(status_code=400, detail="The upload chunk range is invalid.")
    content_length = request.headers.get("content-length")
    if content_length and (not content_length.isdigit() or int(content_length) != expected_size):
        raise HTTPException(status_code=400, detail="The upload chunk size does not match its range.")

    lock_key = f"upload:{upload_id}:chunk-lock"
    lock_value = secrets.token_urlsafe(16)
    try:
        if not redis_client.set(lock_key, lock_value, ex=settings.UPLOAD_SESSION_TTL_SECONDS, nx=True):
            raise HTTPException(status_code=409, detail="Another chunk is still being written.")
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Upload storage is unavailable.") from exc

    root = Path(settings.UPLOAD_DIR) / upload_id
    temporary_path = root / f".chunk-{chunk_index}.tmp"
    source_path = root / f"source{session['extension']}"
    digest = hashlib.sha256()
    received = 0
    try:
        temporary_path.unlink(missing_ok=True)
        with temporary_path.open("xb") as handle:
            async for block in request.stream():
                received += len(block)
                if received > expected_size:
                    raise HTTPException(status_code=413, detail="The upload chunk is larger than allowed.")
                digest.update(block)
                handle.write(block)
            handle.flush()
            os.fsync(handle.fileno())
        if received != expected_size:
            raise HTTPException(status_code=400, detail="The upload chunk is incomplete.")
        chunk_digest = digest.hexdigest()
        previous_digest = session["chunks"][chunk_index]
        if previous_digest and previous_digest != chunk_digest:
            raise HTTPException(status_code=409, detail="A different chunk was already received for this range.")
        with temporary_path.open("rb") as source, source_path.open("r+b") as destination:
            destination.seek(chunk_start)
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                destination.write(block)
            destination.flush()
            os.fsync(destination.fileno())
        temporary_path.unlink(missing_ok=True)
        session["chunks"][chunk_index] = chunk_digest
        now = time.time()
        session["updated_at"] = now
        session_marker = root / ".upload_session"
        session_marker.write_text(str(now), encoding="ascii")
        os.chmod(session_marker, 0o600)
        redis_client.setex(
            _upload_session_key(upload_id),
            settings.UPLOAD_SESSION_TTL_SECONDS,
            json.dumps(session, separators=(",", ":")),
        )
        redis_client.expire("jobs:upload-lock", settings.UPLOAD_SESSION_TTL_SECONDS)
        return {"received": True, "chunk_index": chunk_index, "chunk_count": len(session["chunks"])}
    except HTTPException:
        temporary_path.unlink(missing_ok=True)
        raise
    except (OSError, redis.RedisError) as exc:
        temporary_path.unlink(missing_ok=True)
        raise HTTPException(status_code=503, detail="Upload chunk could not be stored.") from exc
    finally:
        _release_owned_lock(lock_key, lock_value)


@api.post("/uploads/{upload_id}/complete")
async def complete_upload_session(upload_id: str, request: Request) -> dict:
    owner = request.state.principal
    session = _load_upload_session(upload_id, owner)
    lock_key = f"upload:{upload_id}:complete-lock"
    lock_value = secrets.token_urlsafe(16)
    try:
        if not redis_client.set(lock_key, lock_value, ex=300, nx=True):
            raise HTTPException(status_code=409, detail="The upload is already being completed.")
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Upload storage is unavailable.") from exc
    try:
        if redis_client.get("jobs:upload-lock") != upload_id:
            raise HTTPException(status_code=410, detail="The upload session expired. Start the upload again.")
        if redis_client.get(f"upload:{upload_id}:chunk-lock"):
            raise HTTPException(status_code=409, detail="Wait for the current chunk to finish uploading.")
        missing = sum(item is None for item in session["chunks"])
        if missing:
            raise HTTPException(status_code=409, detail=f"{missing} upload chunk(s) are still missing.")
        _ensure_queue_room()
        root = Path(settings.UPLOAD_DIR) / upload_id
        source_path = root / f"source{session['extension']}"
        if not source_path.is_file() or source_path.stat().st_size != session["size"]:
            _discard_upload_session(upload_id)
            raise HTTPException(status_code=400, detail="The uploaded video could not be assembled.")
        from app.analysis import probe_media
        try:
            metadata = await asyncio.to_thread(probe_media, source_path)
        except AnalysisError as exc:
            _discard_upload_session(upload_id)
            raise HTTPException(status_code=415, detail=str(exc)) from exc

        now_epoch = time.time()
        (root / ".created_at").write_text(str(now_epoch), encoding="ascii")
        (root / ".upload_session").unlink(missing_ok=True)
        now = datetime.fromtimestamp(now_epoch, UTC).isoformat()
        job = {
            "id": upload_id,
            "owner": session["owner"],
            "status": "queued",
            "phase": "quick",
            "progress": 0,
            "created_at": now,
            "expires_at": datetime.fromtimestamp(now_epoch + settings.RETENTION_SECONDS, UTC).isoformat(),
            "recording_datetime": session["recording_datetime"],
            "search_area": session["search_area"],
            "time_uncertainty_seconds": session["time_uncertainty_seconds"],
            "flight_lookup_consent": session["flight_lookup_consent"],
            "source_type": "upload",
            "media": metadata,
            "attempt": 1,
            "cancel_requested": False,
        }
        try:
            _save_job(job)
            redis_client.rpush("jobs:queue", upload_id)
        except redis.RedisError as exc:
            redis_client.delete(f"job:{upload_id}")
            raise HTTPException(status_code=503, detail="The video was validated but could not be queued.") from exc
        _finish_upload_session(upload_id)
        return _public_job(job)
    finally:
        _release_owned_lock(lock_key, lock_value)


@api.delete("/uploads/{upload_id}")
def cancel_upload_session(upload_id: str, request: Request) -> dict:
    owner = request.state.principal
    _load_upload_session(upload_id, owner)
    if redis_client.get(f"upload:{upload_id}:chunk-lock") or redis_client.get(
        f"upload:{upload_id}:complete-lock"
    ):
        raise HTTPException(status_code=409, detail="Wait for the current upload request to finish before cancelling.")
    _discard_upload_session(upload_id)
    return {"deleted": True}


@app.get("/health", include_in_schema=False)
def health() -> JSONResponse:
    try:
        redis_client.ping()
    except redis.RedisError:
        return JSONResponse({"ok": False, "dependency": "unavailable"}, status_code=503)
    return JSONResponse({"ok": True})


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home(request: Request, _user: str = Depends(require_access)) -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html", media_type="text/html")


@app.get("/how-it-works", response_class=HTMLResponse, include_in_schema=False)
def how_it_works(_user: str = Depends(require_access)) -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "how-it-works.html", media_type="text/html")


@app.get("/how-it-works.js", include_in_schema=False)
def how_it_works_script(_user: str = Depends(require_access)) -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "how-it-works.js", media_type="text/javascript")


@app.get("/app.js", include_in_schema=False)
def frontend_script(_user: str = Depends(require_access)) -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "app.js", media_type="text/javascript")


@app.get("/styles.css", include_in_schema=False)
def frontend_styles(_user: str = Depends(require_access)) -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "styles.css", media_type="text/css")


@api.post("/jobs")
async def create_job(
    request: Request,
    video: Annotated[UploadFile, File()],
    recording_datetime: Annotated[str, Form()] = "",
    center_lat: Annotated[str, Form()] = "",
    center_lon: Annotated[str, Form()] = "",
    search_radius_km: Annotated[str, Form()] = "",
    time_uncertainty_seconds: Annotated[int, Form()] = 30,
    flight_lookup_consent: Annotated[bool, Form()] = False,
) -> dict:
    owner = request.state.principal
    _rate_limit(owner, "upload", 3, 3600)
    filename = Path((video.filename or "upload").replace("\\", "/")).name
    extension = Path(filename).suffix.lower()
    if extension not in settings.ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Choose an MP4, MOV, MKV, or WebM video.")
    if len(recording_datetime) > 80:
        raise HTTPException(status_code=400, detail="Recording time is too long.")
    recording_datetime = recording_datetime.strip()
    if recording_datetime:
        try:
            parsed_time = datetime.fromisoformat(recording_datetime)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Use an ISO 8601 recording time with a UTC offset.") from exc
        if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
            raise HTTPException(status_code=400, detail="Include the recording time's UTC offset; the app will not infer it.")
        recording_datetime = parsed_time.astimezone(UTC).isoformat()
    if not 1 <= time_uncertainty_seconds <= 600:
        raise HTTPException(status_code=400, detail="Recording-time uncertainty must be between 1 and 600 seconds.")
    region_values = [center_lat.strip(), center_lon.strip(), search_radius_km.strip()]
    if any(len(value) > 40 for value in region_values):
        raise HTTPException(status_code=400, detail="Search-area values are too long.")
    search_area = None
    if any(region_values):
        if not all(region_values):
            raise HTTPException(status_code=400, detail="Provide latitude, longitude, and search radius together.")
        try:
            validated_area = validate_search_area(*region_values)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        search_area = {
            "center_lat": validated_area.center_lat,
            "center_lon": validated_area.center_lon,
            "radius_km": validated_area.radius_km,
        }
    if flight_lookup_consent and (not recording_datetime or not search_area):
        raise HTTPException(
            status_code=400,
            detail="Enter the recording time and bounded search area before requesting historical flight data.",
        )
    _ensure_queue_room()
    lock_value = secrets.token_urlsafe(16)
    try:
        if not redis_client.set("jobs:upload-lock", lock_value, ex=600, nx=True):
            raise HTTPException(status_code=429, detail="Another upload is being prepared. Please try again shortly.")
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Job storage is unavailable.") from exc

    job_id = secrets.token_urlsafe(32)
    upload_path: Path | None = None
    try:
        upload_path = await _save_upload(video, job_id)
        from app.analysis import probe_media
        try:
            metadata = await asyncio.to_thread(probe_media, upload_path)
        except AnalysisError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        now = datetime.now(UTC).isoformat()
        job = {
            "id": job_id,
            "owner": _owner_hash(owner),
            "status": "queued",
            "phase": "quick",
            "progress": 0,
            "created_at": now,
            "expires_at": datetime.fromtimestamp(time.time() + settings.RETENTION_SECONDS, UTC).isoformat(),
            "recording_datetime": recording_datetime.strip() or None,
            "search_area": search_area,
            "time_uncertainty_seconds": time_uncertainty_seconds,
            "flight_lookup_consent": flight_lookup_consent,
            "source_type": "upload",
            "media": metadata,
            "attempt": 1,
            "cancel_requested": False,
        }
        _save_job(job)
        redis_client.rpush("jobs:queue", job_id)
        return _public_job(job)
    except HTTPException:
        if upload_path:
            shutil.rmtree(upload_path.parent, ignore_errors=True)
        raise
    except redis.RedisError as exc:
        if upload_path:
            shutil.rmtree(upload_path.parent, ignore_errors=True)
        raise HTTPException(status_code=503, detail="Job storage is unavailable.") from exc
    finally:
        try:
            redis_client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                1,
                "jobs:upload-lock",
                lock_value,
            )
        except redis.RedisError:
            pass


@api.post("/url-import")
async def create_url_import(
    request: Request,
    url: Annotated[str, Form()],
    recording_datetime: Annotated[str, Form()] = "",
    center_lat: Annotated[str, Form()] = "",
    center_lon: Annotated[str, Form()] = "",
    search_radius_km: Annotated[str, Form()] = "",
    time_uncertainty_seconds: Annotated[int, Form()] = 30,
    flight_lookup_consent: Annotated[bool, Form()] = False,
) -> dict:
    owner = request.state.principal
    _rate_limit(owner, "url-import", 3, 3600)
    try:
        safe_url = await asyncio.to_thread(validate_media_url, url)
    except UnsafeMediaURL as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if len(recording_datetime) > 80:
        raise HTTPException(status_code=400, detail="Recording time is too long.")
    recording_datetime = recording_datetime.strip()
    if recording_datetime:
        try:
            parsed_time = datetime.fromisoformat(recording_datetime)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Use an ISO 8601 recording time with a UTC offset.") from exc
        if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
            raise HTTPException(status_code=400, detail="Include the recording time's UTC offset; the app will not infer it.")
        recording_datetime = parsed_time.astimezone(UTC).isoformat()
    if not 1 <= time_uncertainty_seconds <= 600:
        raise HTTPException(status_code=400, detail="Recording-time uncertainty must be between 1 and 600 seconds.")
    region_values = [center_lat.strip(), center_lon.strip(), search_radius_km.strip()]
    if any(len(value) > 40 for value in region_values):
        raise HTTPException(status_code=400, detail="Search-area values are too long.")
    search_area = None
    if any(region_values):
        if not all(region_values):
            raise HTTPException(status_code=400, detail="Provide latitude, longitude, and search radius together.")
        try:
            validated_area = validate_search_area(*region_values)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        search_area = {
            "center_lat": validated_area.center_lat,
            "center_lon": validated_area.center_lon,
            "radius_km": validated_area.radius_km,
        }
    if flight_lookup_consent and (not recording_datetime or not search_area):
        raise HTTPException(
            status_code=400,
            detail="Enter the recording time and bounded search area before requesting historical flight data.",
        )
    _ensure_queue_room()
    lock_value = secrets.token_urlsafe(16)
    try:
        if not redis_client.set("jobs:upload-lock", lock_value, ex=600, nx=True):
            raise HTTPException(status_code=429, detail="Another video is being prepared. Please try again shortly.")
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Job storage is unavailable.") from exc

    job_id = secrets.token_urlsafe(32)
    root = Path(settings.UPLOAD_DIR) / job_id
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(root, 0o700)
        marker = root / ".created_at"
        marker.write_text(str(time.time()), encoding="ascii")
        os.chmod(marker, 0o600)
        url_file = root / ".source_url"
        descriptor = os.open(url_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(safe_url)
        now = datetime.now(UTC).isoformat()
        job = {
            "id": job_id,
            "owner": _owner_hash(owner),
            "status": "queued",
            "phase": "import",
            "progress": 0,
            "created_at": now,
            "expires_at": datetime.fromtimestamp(time.time() + settings.RETENTION_SECONDS, UTC).isoformat(),
            "recording_datetime": recording_datetime or None,
            "search_area": search_area,
            "time_uncertainty_seconds": time_uncertainty_seconds,
            "flight_lookup_consent": flight_lookup_consent,
            "source_type": "url",
            "media": None,
            "attempt": 1,
            "cancel_requested": False,
        }
        _save_job(job)
        redis_client.rpush("jobs:queue", job_id)
        return _public_job(job)
    except HTTPException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    except (OSError, redis.RedisError) as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(status_code=503, detail="Video import could not be queued.") from exc
    finally:
        try:
            redis_client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                1,
                "jobs:upload-lock",
                lock_value,
            )
        except redis.RedisError:
            pass


@api.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict:
    owner = request.state.principal
    _rate_limit(owner, "read", 120, 60)
    return _public_job(_load_job(job_id, owner))


@api.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request) -> dict:
    owner = request.state.principal
    _rate_limit(owner, "cancel", 30, 60)
    job = _load_job(job_id, owner)
    if job["status"] == "queued":
        job["status"] = "cancelled"
        job["cancel_requested"] = True
        shutil.rmtree(Path(settings.UPLOAD_DIR) / job_id, ignore_errors=True)
    elif job["status"] == "processing":
        job["cancel_requested"] = True
    elif job["status"] not in {"cancelled", "failed"}:
        raise HTTPException(status_code=409, detail="This job can no longer be cancelled.")
    _save_job(job)
    return _public_job(job)


@api.post("/jobs/{job_id}/deep")
def start_deep_scan(
    job_id: str,
    request: Request,
    manual_override: bool = Form(default=False),
    recording_datetime: str = Form(default=""),
    center_lat: str = Form(default=""),
    center_lon: str = Form(default=""),
    search_radius_km: str = Form(default=""),
    time_uncertainty_seconds: int | None = Form(default=None),
    flight_lookup_consent: bool | None = Form(default=None),
) -> dict:
    owner = request.state.principal
    _rate_limit(owner, "deep", 5, 3600)
    job = _load_job(job_id, owner)
    if job["status"] not in {"completed", "failed"} or job.get("phase") == "deep":
        raise HTTPException(status_code=409, detail="This job is not ready for deep analysis.")
    qualification = (job.get("quick_result") or {}).get("qualification")
    if qualification not in {"likely_suitable", "inconclusive", "unsuitable"}:
        raise HTTPException(status_code=409, detail="Quick screening must complete before deep analysis.")
    if qualification == "unsuitable":
        raise HTTPException(status_code=409, detail="Deep analysis is unavailable for an unsuitable recording.")
    if qualification == "inconclusive" and not manual_override:
        raise HTTPException(status_code=409, detail="Confirm the manual override to analyze an inconclusive recording.")
    # The time and area may be supplied here, after the quick check, instead of at
    # upload. Sending the consent field replaces whatever the job held before.
    if flight_lookup_consent is not None:
        recorded, area, uncertainty, consent = _validated_scan_options(
            recording_datetime,
            center_lat,
            center_lon,
            search_radius_km,
            30 if time_uncertainty_seconds is None else time_uncertainty_seconds,
            flight_lookup_consent,
        )
        job["recording_datetime"] = recorded
        job["search_area"] = area
        job["time_uncertainty_seconds"] = uncertainty
        job["flight_lookup_consent"] = consent
    _ensure_queue_room()
    job["phase"] = "deep"
    job["status"] = "queued"
    job["progress"] = 0
    job["error"] = None
    job["cancel_requested"] = False
    job["attempt"] = 1
    _save_job(job)
    redis_client.rpush("jobs:queue", job_id)
    return _public_job(job)


@api.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, request: Request) -> dict:
    owner = request.state.principal
    _rate_limit(owner, "retry", 5, 3600)
    job = _load_job(job_id, owner)
    if job["status"] != "failed" or int(job.get("attempt", 1)) >= 2:
        raise HTTPException(status_code=409, detail="This job cannot be retried.")
    _ensure_queue_room()
    job["attempt"] = int(job.get("attempt", 1)) + 1
    job["status"] = "queued"
    job["progress"] = 0
    job["error"] = None
    job["cancel_requested"] = False
    _save_job(job)
    redis_client.rpush("jobs:queue", job_id)
    return _public_job(job)


@api.get("/jobs/{job_id}/spectrogram")
def spectrogram(job_id: str, request: Request) -> Response:
    owner = request.state.principal
    _rate_limit(owner, "plot", 60, 60)
    job = _load_job(job_id, owner)
    filename = "deep.svg" if job.get("deep_result") else "quick.svg"
    path = Path(settings.DATA_DIR) / job_id / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Spectrogram is not ready.")
    return FileResponse(path, media_type="image/svg+xml", headers={"Cache-Control": "private, no-store"})


@api.get("/jobs/{job_id}/report")
def report(job_id: str, request: Request) -> Response:
    owner = request.state.principal
    _rate_limit(owner, "report", 20, 60)
    job = _load_job(job_id, owner)
    payload = {
        "application": "DopplerTrack",
        "generated_at": datetime.now(UTC).isoformat(),
        "job": _public_job(job),
        "disclaimer": "Acoustic screening and spectral ridge measurements are experimental and do not confirm aircraft, flight identity, or location.",
    }
    body = safe_report_json(payload)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{report_filename(job_id)}"',
            "Cache-Control": "private, no-store",
        },
    )


app.include_router(api)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; media-src blob:; style-src 'self'; script-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    )
    response.headers.setdefault("Cache-Control", "private, no-store")
    return response
